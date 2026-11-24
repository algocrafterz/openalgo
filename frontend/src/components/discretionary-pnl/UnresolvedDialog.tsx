import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import {
  type DiscretionaryFilters,
  discretionaryPnlApi,
  type UnresolvedFill,
} from '@/api/discretionaryPnl'
import { Alert, AlertDescription } from '@/components/ui/alert'
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
import { fmtPrice, fmtQty, fmtStamp, reasonText } from '@/lib/discretionaryPnlView'

function FillRows({ title, rows }: { title: string; rows: UnresolvedFill[] }) {
  if (rows.length === 0) return null
  return (
    <div>
      <div className="text-sm font-medium mb-2">
        {title} ({rows.length})
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Broker</TableHead>
            <TableHead>Account</TableHead>
            <TableHead>Symbol</TableHead>
            <TableHead>Product</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead>Order</TableHead>
            <TableHead>Why</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, i) => (
            <TableRow key={`${row.id ?? row.fill_ref ?? 'row'}-${i}`}>
              <TableCell className="font-mono">{fmtStamp(row.fill_time)}</TableCell>
              <TableCell>{row.broker || '—'}</TableCell>
              <TableCell>{row.account || '—'}</TableCell>
              <TableCell className="font-medium">{row.symbol || '—'}</TableCell>
              <TableCell>{row.product || '—'}</TableCell>
              <TableCell>{row.side || '—'}</TableCell>
              <TableCell className="text-right font-mono">{fmtQty(row.qty)}</TableCell>
              <TableCell className="text-right font-mono">{fmtPrice(row.price)}</TableCell>
              <TableCell className="font-mono text-xs">{row.order_id || '—'}</TableCell>
              <TableCell className="text-xs">
                {reasonText(row.reason ?? row.attribution_state, row.coverage_from)}
                {row.source_evidence && (
                  <div className="text-muted-foreground" data-testid="unresolved-evidence">
                    Source evidence: {row.source_evidence}
                  </div>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * The fills the ledger could not attribute, or the book could not place. They are never counted
 * in P&L. Opened from the warning above the figures, and read only while open.
 */
export function UnresolvedDialog({
  open,
  onClose,
  filters,
}: {
  open: boolean
  onClose: () => void
  filters: DiscretionaryFilters
}) {
  const query = useQuery({
    queryKey: ['discretionary-pnl', 'unresolved', filters],
    queryFn: () => discretionaryPnlApi.unresolved(filters),
    enabled: open,
    retry: false,
  })
  const unresolved = query.data?.unresolved ?? []
  const orphans = query.data?.orphans ?? []
  const count = (query.data?.unresolved_count ?? 0) + (query.data?.orphans_count ?? 0)

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-5xl"
        data-testid="unresolved-dialog"
      >
        <DialogHeader>
          <DialogTitle>Fills left out of your figures</DialogTitle>
          <DialogDescription>
            Not counted in trade statistics until attributed. Covers all dates for the current
            filters.
          </DialogDescription>
        </DialogHeader>
        {query.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : query.error ? (
          <Alert variant="destructive" data-testid="unresolved-error-indicator">
            <AlertTriangle className="h-4 w-4" />
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
        ) : count === 0 ? (
          <p className="text-sm text-muted-foreground">No unresolved fills.</p>
        ) : (
          <div className="min-w-0 space-y-4 overflow-x-auto">
            <FillRows title="Unresolved" rows={unresolved} />
            <FillRows title="Could not be placed" rows={orphans} />
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
