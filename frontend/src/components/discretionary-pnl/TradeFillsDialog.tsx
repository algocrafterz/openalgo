import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { discretionaryPnlApi } from '@/api/discretionaryPnl'
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
import { fmtPrice, fmtQty, fmtStamp } from '@/lib/discretionaryPnlView'
import { fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

/** A trade picked in the table, with the build of the summary it came from. */
export interface SelectedTrade {
  id: number
  buildId: string
}

/**
 * The ledger fills behind one completed trade (every leg): entries and exits, with the charge
 * each carried.
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
            The broker executions this trade was built from, across all its legs (partial exits).
            Charges are estimates split across the fills.
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
          <div className="space-y-3">
            {trade && (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span>
                  {trade.broker}
                  {trade.account ? ` / ${trade.account}` : ''}
                </span>
                <span>{trade.product}</span>
                <span>Held {fmtDuration(trade.hold_minutes)}</span>
                <span className={cn('font-mono', pnlClass(trade.realized_pnl))}>
                  Gross {formatCurrency(trade.realized_pnl)}
                </span>
                {trade.net_pnl != null && (
                  <span className={cn('font-mono', pnlClass(trade.net_pnl))}>
                    Net {formatCurrency(trade.net_pnl)}
                  </span>
                )}
              </div>
            )}
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Role</TableHead>
                  <TableHead>Time</TableHead>
                  <TableHead>Side</TableHead>
                  <TableHead className="text-right">Qty used</TableHead>
                  <TableHead className="text-right">Price</TableHead>
                  <TableHead className="text-right">Charge</TableHead>
                  <TableHead>Order</TableHead>
                  <TableHead>Attribution</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.fills.map((fill) => (
                  <TableRow key={`${fill.role}-${fill.fill_ref}`}>
                    <TableCell>
                      <Badge variant="outline">{fill.role === 'ENTRY' ? 'Entry' : 'Exit'}</Badge>
                    </TableCell>
                    <TableCell className="font-mono">{fmtStamp(fill.fill_time)}</TableCell>
                    <TableCell>{fill.side}</TableCell>
                    <TableCell className="text-right font-mono">{fmtQty(fill.qty_used)}</TableCell>
                    <TableCell className="text-right font-mono">{fmtPrice(fill.price)}</TableCell>
                    <TableCell className="text-right font-mono text-muted-foreground">
                      {fill.charge_allocated == null ? '—' : formatCurrency(fill.charge_allocated)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{fill.order_id || '—'}</TableCell>
                    <TableCell className="text-xs">{fill.attribution_state}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}
