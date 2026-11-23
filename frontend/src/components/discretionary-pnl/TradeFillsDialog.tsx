import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { useId, useMemo, useState } from 'react'
import { discretionaryPnlApi, type TradeFill } from '@/api/discretionaryPnl'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { buildLifecycle, fmtNet, type LifecycleEvent } from '@/lib/discretionaryLifecycle'
import { fmtPrice, fmtQty, pnlTextClass } from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

/** A trade picked in the table, with the build of the summary it came from. */
export interface SelectedTrade {
  id: number
  buildId: string
}

/**
 * The ledger fills behind one completed trade: every fill that opened, added to, reduced or
 * closed it, with the charge each carried, as a chronological lifecycle (see buildLifecycle).
 * Trade ids are reassigned by a rebuild, so the request carries the build_id of the summary the
 * row came from and the cache key includes it: a changed build is reported, never reused.
 */
export function TradeFillsDialog({
  selected,
  onClose,
  formatCurrency,
}: {
  selected: SelectedTrade | null
  onClose: () => void
  formatCurrency: (value: number) => string
}) {
  const queryClient = useQueryClient()
  const open = selected !== null
  const query = useQuery({
    queryKey: ['discretionary-pnl', 'fills', selected?.buildId, selected?.id],
    queryFn: () =>
      discretionaryPnlApi.tradeFills(selected?.id as number, selected?.buildId as string),
    enabled: open,
    staleTime: 60_000,
    retry: false,
  })
  const trade = query.data?.trade
  const changed = (query.error as { code?: string } | null)?.code === 'build_changed'

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {trade ? `${trade.symbol} ${trade.direction === 'LONG' ? 'long' : 'short'}` : 'Trade'}{' '}
            fills
          </DialogTitle>
          <DialogDescription>
            Fills by broker order, with net position after each. Charges are estimated.
          </DialogDescription>
        </DialogHeader>
        {query.isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : changed ? (
          <Alert variant="warning">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>Data changed</AlertTitle>
            <AlertDescription>
              <p>The data changed since this page loaded. Refresh to see the current trades.</p>
              <Button
                type="button"
                size="sm"
                className="mt-2"
                onClick={() => {
                  queryClient.invalidateQueries({ queryKey: ['discretionary-pnl'] })
                  onClose()
                }}
              >
                Refresh
              </Button>
            </AlertDescription>
          </Alert>
        ) : query.error ? (
          <Alert variant="destructive">
            <AlertTitle>Could not load the fills</AlertTitle>
            <AlertDescription>
              <p>{(query.error as Error).message}</p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="mt-2"
                onClick={() => query.refetch()}
              >
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : query.data ? (
          <div className="min-w-0 space-y-3">
            {trade && (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span>
                  {trade.broker}
                  {trade.account ? ` / ${trade.account}` : ''}
                </span>
                <span>{trade.product}</span>
                <span>Held {fmtDuration(trade.hold_minutes)}</span>
                <PeakPosition fills={query.data.fills} />
                <span className={cn('font-mono', pnlTextClass(trade.realized_pnl))}>
                  Gross {formatCurrency(trade.realized_pnl)}
                </span>
                {trade.net_pnl != null && (
                  <span className={cn('font-mono', pnlTextClass(trade.net_pnl))}>
                    Net {formatCurrency(trade.net_pnl)}
                  </span>
                )}
              </div>
            )}
            <LifecycleTable
              fills={query.data.fills}
              tradeId={trade?.id ?? selected?.id ?? 0}
              formatCurrency={formatCurrency}
            />
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

/** "14:05:09" of a "YYYY-MM-DD HH:MM:SS" stamp; the date too when the trade spans days. */
function clock(value: string | null, withDate: boolean): string {
  if (!value) return '—'
  const t = value.replace('T', ' ')
  return withDate ? t.slice(0, 19) : t.slice(11, 19)
}

/** Highest absolute running position, shown only when the fills list is whole. */
function PeakPosition({ fills }: { fills: TradeFill[] }) {
  const peak = useMemo(() => buildLifecycle(fills).peakPosition, [fills])
  if (peak == null) return null
  return (
    <span data-testid="lifecycle-peak">
      Peak position in this trade <span className="font-mono">{fmtQty(peak)}</span>
    </span>
  )
}

/**
 * One row per broker order (chronological, with the net position after it); an order filled in
 * parts has a disclosure button that shows its individual fills nested beneath it, with their
 * charge, fill id and attribution state.
 */
function LifecycleTable({
  fills,
  tradeId,
  formatCurrency,
}: {
  fills: TradeFill[]
  tradeId: number
  formatCurrency: (value: number) => string
}) {
  const lifecycle = useMemo(() => buildLifecycle(fills), [fills])
  const baseId = useId()
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const days = new Set(lifecycle.events.map((e) => (e.startTime ?? '').slice(0, 10)))
  const withDate = days.size > 1
  const money = (v: number | null) => (v == null ? '—' : formatCurrency(v))
  return (
    <Table data-testid="lifecycle-table" aria-label={`Position lifecycle of trade ${tradeId}`}>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Action</TableHead>
          <TableHead className="text-right">Quantity</TableHead>
          <TableHead className="text-right">Price</TableHead>
          <TableHead className="text-right">Net position after</TableHead>
          <TableHead className="text-right">Charge (est.)</TableHead>
          <TableHead>Order / fill</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {lifecycle.events.map((e, i) => {
          const rowsId = `${baseId}-fills-${i}`
          const expanded = !!open[e.key]
          const multi = e.fills.length > 1
          return (
            <LifecycleRows
              key={e.key}
              event={e}
              rowsId={rowsId}
              expanded={expanded}
              multi={multi}
              withDate={withDate}
              money={money}
              onToggle={() => setOpen((o) => ({ ...o, [e.key]: !o[e.key] }))}
            />
          )
        })}
      </TableBody>
    </Table>
  )
}

function LifecycleRows({
  event: e,
  rowsId,
  expanded,
  multi,
  withDate,
  money,
  onToggle,
}: {
  event: LifecycleEvent<TradeFill>
  rowsId: string
  expanded: boolean
  multi: boolean
  withDate: boolean
  money: (v: number | null) => string
  onToggle: () => void
}) {
  const time =
    e.endTime && e.endTime !== e.startTime
      ? `${clock(e.startTime, withDate)} - ${clock(e.endTime, false)}`
      : clock(e.startTime, withDate)
  const singleTitle = e.fills[0].attribution_state
    ? `Attribution: ${e.fills[0].attribution_state}`
    : undefined
  return (
    <>
      <TableRow data-testid="lifecycle-event">
        <TableCell className="whitespace-nowrap font-mono tabular-nums">{time}</TableCell>
        <TableCell className="whitespace-nowrap">
          <Badge variant="outline" data-testid="lifecycle-label">
            {e.label}
          </Badge>{' '}
          <span className="text-xs text-muted-foreground">{e.side === 'BUY' ? 'Buy' : 'Sell'}</span>
        </TableCell>
        <TableCell className="text-right font-mono tabular-nums">
          {fmtQty(e.quantity)}
          {multi && (
            <button
              type="button"
              className="ml-1 inline-flex items-center gap-0.5 rounded px-1 font-sans text-xs text-muted-foreground underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2"
              aria-expanded={expanded}
              aria-controls={e.fills.map((_, j) => `${rowsId}-${j}`).join(' ')}
              aria-label={`${expanded ? 'Hide' : 'Show'} the ${e.fills.length} fills of this order`}
              onClick={onToggle}
            >
              {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
              {e.fills.length} fills
            </button>
          )}
        </TableCell>
        <TableCell className="text-right font-mono tabular-nums">{fmtPrice(e.price)}</TableCell>
        <TableCell
          className="text-right font-mono tabular-nums font-medium"
          data-testid="lifecycle-net"
        >
          {fmtNet(e.netAfter)}
        </TableCell>
        <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
          {money(e.charges)}
        </TableCell>
        <TableCell className="font-mono text-xs" title={multi ? undefined : singleTitle}>
          {e.orderId || '—'}
          {!multi && e.fills[0].fill_id && (
            <span className="block text-muted-foreground">fill {e.fills[0].fill_id}</span>
          )}
        </TableCell>
      </TableRow>
      {(multi ? expanded : false) &&
        e.fills.map((f, j) => (
          <TableRow
            key={`${f.role}-${f.fill_ref}`}
            id={`${rowsId}-${j}`}
            className="bg-muted/30"
            data-testid="lifecycle-fill"
          >
            <TableCell className="whitespace-nowrap pl-6 font-mono text-xs tabular-nums">
              {clock(f.fill_time, withDate)}
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">
              {f.side} fill{f.attribution_state ? `, ${f.attribution_state}` : ''}
            </TableCell>
            <TableCell className="text-right font-mono text-xs tabular-nums">
              {fmtQty(f.qty_used)}
            </TableCell>
            <TableCell className="text-right font-mono text-xs tabular-nums">
              {fmtPrice(f.price)}
            </TableCell>
            <TableCell />
            <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
              {money(f.charge_allocated)}
            </TableCell>
            <TableCell className="font-mono text-xs">{f.fill_id || f.fill_ref}</TableCell>
          </TableRow>
        ))}
    </>
  )
}
